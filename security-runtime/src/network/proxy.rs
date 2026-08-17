use base64::engine::general_purpose::{STANDARD as BASE64_STANDARD, URL_SAFE_NO_PAD};
use base64::Engine;
use rand::RngCore;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{IpAddr, Ipv4Addr, Shutdown, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use subtle::ConstantTimeEq;

use super::connector;
use super::policy::{NetworkError, NetworkPolicy};
use crate::protocol::NetworkErrorCode;

const MAX_HEADER: usize = 64 * 1024;
const MAX_CONNECTIONS: usize = 64;
const IO_TIMEOUT: Duration = Duration::from_secs(30);
const HTTP_MAX_REQUEST_BYTES: usize = 16 * 1024 * 1024;
const HTTP_MAX_RESPONSE_BYTES: u64 = 100 * 1024 * 1024;
const HTTP_MAX_LIFETIME: Duration = Duration::from_secs(120);
// CONNECT tunnel hard caps (N6). Previously `tunnel` ran `io::copy` in both
// directions with no total lifetime or byte ceiling: combined with
// MAX_CONNECTIONS=64 a slow peer could pin all proxy slots indefinitely.
// spec §7.3 caps foreground tunnels at 10 min and background at 30 min; the
// proxy cannot tell which scenario it is in, so we enforce the stricter
// foreground ceiling here. Hosts that need the background budget must raise
// this constant (or thread a flag through `ProxyHandle::start` later).
// ponytail: single conservative constant; per-scenario config when a real
// background caller needs it.
const TUNNEL_MAX_LIFETIME: Duration = Duration::from_secs(600);
// Byte ceiling per direction. Not in the spec, but without it a tunnel can
// stream unbounded data within the lifetime window. 256 MiB covers typical
// HTTP/HTTPS payloads while still bounding memory/bandwidth abuse.
const TUNNEL_MAX_BYTES: u64 = 256 * 1024 * 1024;
type ActiveConnections = Arc<Mutex<HashMap<usize, Vec<TcpStream>>>>;

pub struct ProxyHandle {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    address: SocketAddr,
    stopped: Arc<AtomicBool>,
    connections: ActiveConnections,
    password: String,
    #[cfg(target_os = "linux")]
    authorization_header: String,
}

impl ProxyHandle {
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    pub fn start(policy: NetworkPolicy) -> Result<Self, NetworkError> {
        Self::start_on(policy, 0)
    }

    pub fn start_on(policy: NetworkPolicy, port: u16) -> Result<Self, NetworkError> {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, port)).map_err(|error| {
            NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                format!("cannot bind managed proxy: {error}"),
            )
        })?;
        #[cfg(any(target_os = "linux", target_os = "macos"))]
        let address = listener.local_addr().map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        listener.set_nonblocking(true).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&stopped);
        let active = Arc::new(AtomicUsize::new(0));
        let next_connection_id = Arc::new(AtomicUsize::new(1));
        let connections: ActiveConnections = Arc::new(Mutex::new(HashMap::new()));
        let accepted_connections = Arc::clone(&connections);
        let mut secret = [0_u8; 32];
        rand::rngs::OsRng.fill_bytes(&mut secret);
        let password = URL_SAFE_NO_PAD.encode(secret);
        let authorization_header = format!(
            "Basic {}",
            BASE64_STANDARD.encode(format!("crew:{password}"))
        );
        let expected_authorization = authorization_header.clone();
        thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((stream, peer)) if peer.ip() == IpAddr::V4(Ipv4Addr::LOCALHOST) => {
                        if active.fetch_add(1, Ordering::AcqRel) >= MAX_CONNECTIONS {
                            active.fetch_sub(1, Ordering::AcqRel);
                            continue;
                        }
                        let connection_id = next_connection_id.fetch_add(1, Ordering::Relaxed);
                        if register_client(
                            &accepted_connections,
                            connection_id,
                            &stream,
                            stop.as_ref(),
                        )
                        .is_err()
                        {
                            active.fetch_sub(1, Ordering::AcqRel);
                            continue;
                        }
                        let policy = policy.clone();
                        let active = Arc::clone(&active);
                        let stop = Arc::clone(&stop);
                        let connections = Arc::clone(&accepted_connections);
                        let expected_authorization = expected_authorization.clone();
                        thread::spawn(move || {
                            let _ = serve(
                                stream,
                                &policy,
                                &expected_authorization,
                                stop.as_ref(),
                                &connections,
                                connection_id,
                            );
                            if let Ok(mut registered) = connections.lock() {
                                registered.remove(&connection_id);
                            }
                            active.fetch_sub(1, Ordering::AcqRel);
                        });
                    }
                    Ok(_) => {}
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(_) => break,
                }
            }
        });
        Ok(Self {
            #[cfg(any(target_os = "linux", target_os = "macos"))]
            address,
            stopped,
            connections,
            password,
            #[cfg(target_os = "linux")]
            authorization_header,
        })
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    pub fn address(&self) -> SocketAddr {
        self.address
    }

    #[cfg(target_os = "linux")]
    pub fn authorization_header(&self) -> &str {
        &self.authorization_header
    }

    pub fn proxy_url(&self, address: SocketAddr) -> String {
        format!("http://crew:{}@{address}", self.password)
    }
}

impl Drop for ProxyHandle {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        if let Ok(mut connections) = self.connections.lock() {
            for streams in connections.values() {
                for stream in streams {
                    let _ = stream.shutdown(Shutdown::Both);
                }
            }
            connections.clear();
        }
    }
}

fn serve(
    mut client: TcpStream,
    policy: &NetworkPolicy,
    expected_authorization: &str,
    stopped: &AtomicBool,
    connections: &ActiveConnections,
    connection_id: usize,
) -> Result<(), NetworkError> {
    client.set_read_timeout(Some(IO_TIMEOUT)).map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    client
        .set_write_timeout(Some(IO_TIMEOUT))
        .map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
    let request_deadline = Instant::now() + HTTP_MAX_LIFETIME;
    let header = read_header(&mut client, request_deadline)?;
    validate_proxy_authorization(&header, expected_authorization)?;
    let first_line = std::str::from_utf8(&header)
        .map_err(|_| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
        })?
        .lines()
        .next()
        .ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy request is empty")
        })?;
    let fields: Vec<&str> = first_line.split_whitespace().collect();
    if fields.len() != 3 {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid HTTP proxy request line",
        ));
    }
    if !valid_method(fields[0]) {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid HTTP method",
        ));
    }
    if !matches!(fields[2], "HTTP/1.0" | "HTTP/1.1") {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid HTTP version",
        ));
    }
    if fields[0].eq_ignore_ascii_case("CONNECT") {
        let (host, port) = split_authority(fields[1], 443)?;
        validate_proxy_host(&header, &host, port, 443)?;
        let mut upstream = connector::connect(policy, &host, port, "https", IO_TIMEOUT)?;
        register_upstream(connections, connection_id, &upstream, stopped)?;
        client
            .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            .map_err(|error| {
                NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
            })?;
        // A standard CONNECT client sends TLS only after receiving 200. The
        // target was already authorized and connected above; do not forward
        // application bytes until the ClientHello SNI matches that target.
        let client_hello = read_validated_tls_client_hello(&mut client, &host, request_deadline)?;
        upstream.write_all(&client_hello).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        return tunnel(client, upstream);
    }
    let (host, port, path) = parse_absolute_http_target(fields[1])?;
    validate_proxy_host(&header, &host, port, 80)?;
    let websocket = websocket_request(&header, fields[0])?;
    let body_length = request_body_length(&header)?;
    if websocket && body_length != 0 {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "WebSocket upgrade request must not have a body",
        ));
    }
    let mut upstream = connector::connect(policy, &host, port, "http", IO_TIMEOUT)?;
    register_upstream(connections, connection_id, &upstream, stopped)?;
    let rewritten = if websocket {
        rewrite_websocket_request_line(&header, fields[0], &path, fields[2])?
    } else {
        rewrite_request_line(&header, fields[0], &path, fields[2])?
    };
    upstream.write_all(&rewritten).map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    copy_request_body(&mut client, &mut upstream, body_length, request_deadline)?;
    if !websocket {
        upstream.shutdown(Shutdown::Write).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
    }
    if websocket {
        let response = read_header(&mut upstream, request_deadline)?;
        validate_websocket_response(&response)?;
        client.write_all(&response).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        return tunnel(client, upstream);
    }
    relay_http_response(&mut upstream, &mut client, request_deadline)
}

fn register_client(
    connections: &ActiveConnections,
    connection_id: usize,
    stream: &TcpStream,
    stopped: &AtomicBool,
) -> Result<(), NetworkError> {
    let tracked = stream.try_clone().map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    let mut registered = connections.lock().map_err(|_| {
        NetworkError::new(
            NetworkErrorCode::NetworkUnavailable,
            "managed proxy connection registry is unavailable",
        )
    })?;
    if stopped.load(Ordering::Acquire) {
        let _ = tracked.shutdown(Shutdown::Both);
        return Err(NetworkError::new(
            NetworkErrorCode::NetworkUnavailable,
            "managed proxy is stopping",
        ));
    }
    registered.insert(connection_id, vec![tracked]);
    Ok(())
}

fn register_upstream(
    connections: &ActiveConnections,
    connection_id: usize,
    stream: &TcpStream,
    stopped: &AtomicBool,
) -> Result<(), NetworkError> {
    let tracked = stream.try_clone().map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    let mut registered = connections.lock().map_err(|_| {
        NetworkError::new(
            NetworkErrorCode::NetworkUnavailable,
            "managed proxy connection registry is unavailable",
        )
    })?;
    if stopped.load(Ordering::Acquire) {
        let _ = tracked.shutdown(Shutdown::Both);
        return Err(NetworkError::new(
            NetworkErrorCode::NetworkUnavailable,
            "managed proxy is stopping",
        ));
    }
    let streams = registered.get_mut(&connection_id).ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::NetworkUnavailable,
            "managed proxy connection attribution is unavailable",
        )
    })?;
    streams.push(tracked);
    Ok(())
}

fn read_header(stream: &mut TcpStream, deadline: Instant) -> Result<Vec<u8>, NetworkError> {
    let mut result = Vec::new();
    let mut byte = [0_u8; 1];
    while result.len() < MAX_HEADER {
        apply_read_deadline(stream, deadline)?;
        let count = stream.read(&mut byte).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        if count == 0 {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "proxy request ended before headers",
            ));
        }
        result.push(byte[0]);
        if result.ends_with(b"\r\n\r\n") {
            return Ok(result);
        }
    }
    Err(NetworkError::new(
        NetworkErrorCode::SandboxDenied,
        "proxy headers exceed limit",
    ))
}

fn apply_read_deadline(stream: &TcpStream, deadline: Instant) -> Result<(), NetworkError> {
    let remaining = deadline.saturating_duration_since(Instant::now());
    if remaining.is_zero() {
        return Err(NetworkError::new(
            NetworkErrorCode::NetworkUnavailable,
            "managed proxy request lifetime exceeded",
        ));
    }
    if remaining < IO_TIMEOUT {
        stream.set_read_timeout(Some(remaining)).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
    }
    Ok(())
}

fn copy_request_body(
    client: &mut TcpStream,
    upstream: &mut TcpStream,
    length: usize,
    deadline: Instant,
) -> Result<(), NetworkError> {
    let mut remaining_body = length;
    let mut buffer = [0_u8; 8192];
    while remaining_body > 0 {
        apply_read_deadline(client, deadline)?;
        let remaining_time = deadline.saturating_duration_since(Instant::now());
        if remaining_time.is_zero() {
            return Err(NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                "managed proxy request lifetime exceeded",
            ));
        }
        upstream
            .set_write_timeout(Some(remaining_time.min(IO_TIMEOUT)))
            .map_err(|error| {
                NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
            })?;
        let amount = remaining_body.min(buffer.len());
        let count = client.read(&mut buffer[..amount]).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        if count == 0 {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "plain HTTP request body ended before Content-Length",
            ));
        }
        upstream.write_all(&buffer[..count]).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        remaining_body -= count;
    }
    Ok(())
}

fn relay_http_response(
    upstream: &mut TcpStream,
    client: &mut TcpStream,
    deadline: Instant,
) -> Result<(), NetworkError> {
    let mut total_header_bytes = 0_u64;
    for _ in 0..16 {
        let header = read_header(upstream, deadline)?;
        total_header_bytes = total_header_bytes
            .checked_add(header.len() as u64)
            .ok_or_else(|| {
                NetworkError::new(
                    NetworkErrorCode::NetworkUnavailable,
                    "upstream response headers exceed limit",
                )
            })?;
        if total_header_bytes > MAX_HEADER as u64 {
            return Err(NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                "upstream response headers exceed limit",
            ));
        }
        let first_line = std::str::from_utf8(&header)
            .ok()
            .and_then(|text| text.split("\r\n").next())
            .ok_or_else(|| {
                NetworkError::new(
                    NetworkErrorCode::NetworkUnavailable,
                    "upstream response status is invalid",
                )
            })?;
        let fields = first_line.split_whitespace().collect::<Vec<_>>();
        if fields.len() < 2
            || !matches!(fields[0], "HTTP/1.0" | "HTTP/1.1")
            || fields[1].len() != 3
            || !fields[1].bytes().all(|byte| byte.is_ascii_digit())
        {
            return Err(NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                "upstream response status is invalid",
            ));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                "managed proxy request lifetime exceeded",
            ));
        }
        client
            .set_write_timeout(Some(remaining.min(IO_TIMEOUT)))
            .map_err(|error| {
                NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
            })?;
        client.write_all(&header).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        let status = fields[1].parse::<u16>().map_err(|_| {
            NetworkError::new(
                NetworkErrorCode::NetworkUnavailable,
                "upstream response status is invalid",
            )
        })?;
        if (100..200).contains(&status) {
            continue;
        }
        let body_limit = HTTP_MAX_RESPONSE_BYTES
            .checked_sub(total_header_bytes)
            .ok_or_else(|| {
                NetworkError::new(
                    NetworkErrorCode::NetworkUnavailable,
                    "upstream response exceeds limit",
                )
            })?;
        return copy_bounded(upstream, client, body_limit, deadline)
            .map(|_| ())
            .map_err(|error| {
                NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
            });
    }
    Err(NetworkError::new(
        NetworkErrorCode::NetworkUnavailable,
        "upstream sent too many informational responses",
    ))
}

fn split_authority(value: &str, default_port: u16) -> Result<(String, u16), NetworkError> {
    if let Some(host) = value.strip_prefix('[') {
        let (host, tail) = host.split_once(']').ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid IPv6 authority")
        })?;
        let port = if tail.is_empty() {
            default_port
        } else {
            tail.strip_prefix(':')
                .ok_or_else(|| {
                    NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid IPv6 authority")
                })?
                .parse()
                .map_err(|_| {
                    NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy port")
                })?
        };
        if port == 0 {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "proxy port must be between 1 and 65535",
            ));
        }
        return Ok((host.to_string(), port));
    }
    let (host, port) = value.rsplit_once(':').ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "proxy target must include a port",
        )
    })?;
    let port = port
        .parse()
        .map_err(|_| NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy port"))?;
    if port == 0 {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "proxy port must be between 1 and 65535",
        ));
    }
    Ok((host.to_string(), port))
}

fn validate_proxy_host(
    header: &[u8],
    expected_host: &str,
    expected_port: u16,
    default_port: u16,
) -> Result<(), NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
    })?;
    let mut values = text.split("\r\n").skip(1).filter_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("host").then_some(value.trim())
    });
    let value = values.next().ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "proxy request requires exactly one Host header",
        )
    })?;
    if values.next().is_some() {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "proxy request requires exactly one Host header",
        ));
    }
    let (host, port) = split_host_header_authority(value, default_port)?;
    if !host.eq_ignore_ascii_case(expected_host) || port != expected_port {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "proxy target and Host header do not match",
        ));
    }
    Ok(())
}

fn validate_proxy_authorization(header: &[u8], expected: &str) -> Result<(), NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
    })?;
    let values = text
        .split("\r\n")
        .skip(1)
        .filter_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("proxy-authorization")
                .then_some(value.trim())
        })
        .collect::<Vec<_>>();
    let valid = values.len() == 1 && bool::from(values[0].as_bytes().ct_eq(expected.as_bytes()));
    if !valid {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "managed proxy authentication failed",
        ));
    }
    Ok(())
}

fn request_body_length(header: &[u8]) -> Result<usize, NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
    })?;
    let mut content_length: Option<&str> = None;
    for line in text.split("\r\n").skip(1) {
        if line.is_empty() {
            break;
        }
        if line.starts_with([' ', '\t']) {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "folded proxy headers are forbidden",
            ));
        }
        let (name, value) = line.split_once(':').ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy header")
        })?;
        if name.eq_ignore_ascii_case("content-length") {
            if content_length.replace(value.trim()).is_some() {
                return Err(NetworkError::new(
                    NetworkErrorCode::SandboxDenied,
                    "ambiguous Content-Length",
                ));
            }
        } else if name.eq_ignore_ascii_case("transfer-encoding") {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "plain HTTP Transfer-Encoding is not supported",
            ));
        } else if name.eq_ignore_ascii_case("expect") {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "plain HTTP Expect is not supported",
            ));
        }
    }
    let Some(raw_length) = content_length else {
        return Ok(0);
    };
    if raw_length.is_empty() || !raw_length.bytes().all(|value| value.is_ascii_digit()) {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid Content-Length",
        ));
    }
    let length = raw_length.parse::<usize>().map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid Content-Length")
    })?;
    if length > HTTP_MAX_REQUEST_BYTES {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "plain HTTP request body exceeds limit",
        ));
    }
    Ok(length)
}

fn valid_method(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 32
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&byte))
}

fn websocket_request(header: &[u8], method: &str) -> Result<bool, NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
    })?;
    let mut upgrades = Vec::new();
    let mut connections = Vec::new();
    for line in text.split("\r\n").skip(1) {
        if line.is_empty() {
            break;
        }
        let (name, value) = line.split_once(':').ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy header")
        })?;
        if name.eq_ignore_ascii_case("upgrade") {
            upgrades.push(value.trim());
        } else if name.eq_ignore_ascii_case("connection") {
            connections.extend(value.split(',').map(str::trim));
        }
    }
    let has_connection_upgrade = connections
        .iter()
        .any(|value| value.eq_ignore_ascii_case("upgrade"));
    if upgrades.is_empty() && !has_connection_upgrade {
        return Ok(false);
    }
    if !method.eq_ignore_ascii_case("GET")
        || upgrades.len() != 1
        || !upgrades[0].eq_ignore_ascii_case("websocket")
        || !connections
            .iter()
            .any(|value| value.eq_ignore_ascii_case("upgrade"))
    {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid WebSocket upgrade request",
        ));
    }
    Ok(true)
}

fn split_host_header_authority(
    value: &str,
    default_port: u16,
) -> Result<(String, u16), NetworkError> {
    if value.is_empty()
        || value.contains(['/', '\\', '@'])
        || value.bytes().any(|byte| byte <= 0x20 || byte == 0x7f)
    {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid proxy Host header",
        ));
    }
    if value.starts_with('[') {
        return split_authority(value, default_port);
    }
    if let Some((host, raw_port)) = value.rsplit_once(':') {
        if host.is_empty() || host.contains(':') {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "invalid proxy Host header",
            ));
        }
        let port = raw_port.parse().map_err(|_| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy Host port")
        })?;
        if port == 0 {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "proxy port must be between 1 and 65535",
            ));
        }
        return Ok((host.to_string(), port));
    }
    Ok((value.to_string(), default_port))
}

fn read_validated_tls_client_hello(
    client: &mut TcpStream,
    expected_host: &str,
    deadline: Instant,
) -> Result<Vec<u8>, NetworkError> {
    const MAX_RECORDS_BYTES: usize = 72 * 1024;
    const MAX_RECORD_BYTES: usize = 18 * 1024;
    const MAX_HELLO_BYTES: usize = 64 * 1024;

    let mut records = Vec::new();
    let mut handshake = Vec::new();
    while records.len() <= MAX_RECORDS_BYTES {
        let mut header = [0_u8; 5];
        read_exact_deadline(client, &mut header, deadline)?;
        let length = u16::from_be_bytes([header[3], header[4]]) as usize;
        if header[0] != 22
            || header[1] != 3
            || length == 0
            || length > MAX_RECORD_BYTES
            || records.len() + 5 + length > MAX_RECORDS_BYTES
        {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "CONNECT accepts only a bounded TLS ClientHello",
            ));
        }
        let mut body = vec![0_u8; length];
        read_exact_deadline(client, &mut body, deadline)?;
        records.extend_from_slice(&header);
        records.extend_from_slice(&body);
        handshake.extend_from_slice(&body);
        if handshake.len() < 4 {
            continue;
        }
        if handshake[0] != 1 {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "CONNECT first TLS handshake is not ClientHello",
            ));
        }
        let hello_length = ((handshake[1] as usize) << 16)
            | ((handshake[2] as usize) << 8)
            | handshake[3] as usize;
        if hello_length == 0 || hello_length > MAX_HELLO_BYTES {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "TLS ClientHello exceeds limit",
            ));
        }
        if handshake.len() < 4 + hello_length {
            continue;
        }
        validate_client_hello(&handshake[4..4 + hello_length], expected_host)?;
        return Ok(records);
    }
    Err(NetworkError::new(
        NetworkErrorCode::SandboxDenied,
        "TLS ClientHello exceeds limit",
    ))
}

fn read_exact_deadline(
    stream: &mut TcpStream,
    buffer: &mut [u8],
    deadline: Instant,
) -> Result<(), NetworkError> {
    let mut offset = 0;
    while offset < buffer.len() {
        apply_read_deadline(stream, deadline)?;
        let count = stream.read(&mut buffer[offset..]).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        if count == 0 {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "CONNECT requires a complete TLS ClientHello",
            ));
        }
        offset += count;
    }
    Ok(())
}

fn validate_client_hello(hello: &[u8], expected_host: &str) -> Result<(), NetworkError> {
    fn take<'a>(input: &'a [u8], offset: &mut usize, length: usize) -> Option<&'a [u8]> {
        let end = offset.checked_add(length)?;
        let value = input.get(*offset..end)?;
        *offset = end;
        Some(value)
    }

    fn u16_at(input: &[u8], offset: &mut usize) -> Option<usize> {
        let value = take(input, offset, 2)?;
        Some(u16::from_be_bytes([value[0], value[1]]) as usize)
    }

    let invalid = || {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "TLS ClientHello structure is invalid",
        )
    };
    let mut offset = 0;
    take(hello, &mut offset, 34).ok_or_else(invalid)?;
    let session_length = *take(hello, &mut offset, 1)
        .ok_or_else(invalid)?
        .first()
        .ok_or_else(invalid)? as usize;
    take(hello, &mut offset, session_length).ok_or_else(invalid)?;
    let cipher_length = u16_at(hello, &mut offset).ok_or_else(invalid)?;
    if cipher_length == 0 || cipher_length % 2 != 0 {
        return Err(invalid());
    }
    take(hello, &mut offset, cipher_length).ok_or_else(invalid)?;
    let compression_length = *take(hello, &mut offset, 1)
        .ok_or_else(invalid)?
        .first()
        .ok_or_else(invalid)? as usize;
    if compression_length == 0 {
        return Err(invalid());
    }
    take(hello, &mut offset, compression_length).ok_or_else(invalid)?;
    let extensions_length = u16_at(hello, &mut offset).ok_or_else(invalid)?;
    let extensions = take(hello, &mut offset, extensions_length).ok_or_else(invalid)?;
    if offset != hello.len() {
        return Err(invalid());
    }

    let mut extension_offset = 0;
    let mut server_name: Option<&str> = None;
    while extension_offset < extensions.len() {
        let extension_type = u16_at(extensions, &mut extension_offset).ok_or_else(invalid)?;
        let extension_length = u16_at(extensions, &mut extension_offset).ok_or_else(invalid)?;
        let extension =
            take(extensions, &mut extension_offset, extension_length).ok_or_else(invalid)?;
        if extension_type != 0 {
            continue;
        }
        if server_name.is_some() {
            return Err(invalid());
        }
        let mut name_offset = 0;
        let names_length = u16_at(extension, &mut name_offset).ok_or_else(invalid)?;
        let names = take(extension, &mut name_offset, names_length).ok_or_else(invalid)?;
        if name_offset != extension.len() {
            return Err(invalid());
        }
        let mut names_offset = 0;
        while names_offset < names.len() {
            let name_type = *take(names, &mut names_offset, 1)
                .ok_or_else(invalid)?
                .first()
                .ok_or_else(invalid)?;
            let name_length = u16_at(names, &mut names_offset).ok_or_else(invalid)?;
            let raw_name = take(names, &mut names_offset, name_length).ok_or_else(invalid)?;
            if name_type == 0 {
                if server_name.is_some() {
                    return Err(invalid());
                }
                server_name = Some(std::str::from_utf8(raw_name).map_err(|_| invalid())?);
            }
        }
    }

    let expected_is_ip = expected_host.parse::<IpAddr>().is_ok();
    match server_name {
        Some(name)
            if !name.is_empty()
                && name.is_ascii()
                && !name.bytes().any(|byte| byte <= 0x20 || byte == 0x7f)
                && name
                    .trim_end_matches('.')
                    .eq_ignore_ascii_case(expected_host) =>
        {
            Ok(())
        }
        None if expected_is_ip => Ok(()),
        _ => Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "TLS SNI does not match CONNECT authority",
        )),
    }
}

fn parse_absolute_http_target(value: &str) -> Result<(String, u16, String), NetworkError> {
    let rest = value.strip_prefix("http://").ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "plain HTTP proxy requires an absolute http:// target",
        )
    })?;
    let (authority, path) = rest
        .split_once('/')
        .map(|(a, p)| (a, format!("/{p}")))
        .unwrap_or((rest, "/".to_string()));
    let (host, port) = if authority.contains(':') {
        split_authority(authority, 80)?
    } else {
        (authority.to_string(), 80)
    };
    Ok((host, port, path))
}

fn rewrite_request_line(
    header: &[u8],
    method: &str,
    path: &str,
    version: &str,
) -> Result<Vec<u8>, NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
    })?;
    let mut lines = text.split("\r\n");
    lines.next().ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "missing request-line boundary",
        )
    })?;
    let mut result = format!("{method} {path} {version}\r\n").into_bytes();
    for line in lines {
        if line.is_empty() {
            break;
        }
        if line.starts_with([' ', '\t']) {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "folded proxy headers are forbidden",
            ));
        }
        let (name, _value) = line.split_once(':').ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy header")
        })?;
        if matches!(
            name.to_ascii_lowercase().as_str(),
            "connection"
                | "keep-alive"
                | "proxy-authenticate"
                | "proxy-authorization"
                | "proxy-connection"
                | "te"
                | "trailer"
                | "upgrade"
        ) {
            continue;
        }
        result.extend_from_slice(line.as_bytes());
        result.extend_from_slice(b"\r\n");
    }
    result.extend_from_slice(b"Connection: close\r\n\r\n");
    Ok(result)
}

fn rewrite_websocket_request_line(
    header: &[u8],
    method: &str,
    path: &str,
    version: &str,
) -> Result<Vec<u8>, NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(NetworkErrorCode::SandboxDenied, "proxy header is not UTF-8")
    })?;
    let mut lines = text.split("\r\n");
    lines.next().ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "missing request-line boundary",
        )
    })?;
    let mut result = format!("{method} {path} {version}\r\n").into_bytes();
    for line in lines {
        if line.is_empty() {
            break;
        }
        if line.starts_with([' ', '\t']) {
            return Err(NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "folded proxy headers are forbidden",
            ));
        }
        let (name, _value) = line.split_once(':').ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy header")
        })?;
        if matches!(
            name.to_ascii_lowercase().as_str(),
            "connection"
                | "keep-alive"
                | "proxy-authenticate"
                | "proxy-authorization"
                | "proxy-connection"
                | "te"
                | "trailer"
                | "transfer-encoding"
                | "upgrade"
        ) {
            continue;
        }
        result.extend_from_slice(line.as_bytes());
        result.extend_from_slice(b"\r\n");
    }
    result.extend_from_slice(b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n");
    Ok(result)
}

fn validate_websocket_response(header: &[u8]) -> Result<(), NetworkError> {
    let text = std::str::from_utf8(header).map_err(|_| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "WebSocket response is not UTF-8",
        )
    })?;
    let mut lines = text.split("\r\n");
    let status = lines.next().ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "WebSocket response is empty",
        )
    })?;
    let fields = status.split_whitespace().collect::<Vec<_>>();
    if fields.len() < 2 || fields[1] != "101" {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "upstream did not complete the WebSocket upgrade",
        ));
    }
    let mut upgrades = 0;
    let mut connection_upgrade = false;
    for line in lines {
        if line.is_empty() {
            break;
        }
        let (name, value) = line.split_once(':').ok_or_else(|| {
            NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "invalid WebSocket response",
            )
        })?;
        if name.eq_ignore_ascii_case("upgrade") {
            upgrades += 1;
            if !value.trim().eq_ignore_ascii_case("websocket") {
                return Err(NetworkError::new(
                    NetworkErrorCode::SandboxDenied,
                    "invalid WebSocket upgrade response",
                ));
            }
        } else if name.eq_ignore_ascii_case("connection") {
            connection_upgrade = value
                .split(',')
                .any(|item| item.trim().eq_ignore_ascii_case("upgrade"));
        }
    }
    if upgrades != 1 || !connection_upgrade {
        return Err(NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "invalid WebSocket upgrade response",
        ));
    }
    Ok(())
}

/// Bounded bidirectional copy for CONNECT tunnels (N6).
///
/// Enforces both a total lifetime deadline (spec §7.3) and a per-direction byte
/// ceiling. The stream-level read/write timeouts (`IO_TIMEOUT`) bound individual
/// idle periods; this function bounds the *aggregate* session so a slow-but-
/// steady peer cannot pin a proxy slot for hours.
fn copy_bounded<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    max_bytes: u64,
    deadline: Instant,
) -> std::io::Result<u64> {
    let mut total = 0u64;
    let mut buffer = [0u8; 8192];
    loop {
        if total >= max_bytes {
            return Err(std::io::Error::other("tunnel byte limit exceeded"));
        }
        if Instant::now() >= deadline {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "tunnel lifetime exceeded",
            ));
        }
        let remaining = (max_bytes - total).min(buffer.len() as u64) as usize;
        let count = match reader.read(&mut buffer[..remaining]) {
            Ok(0) => break,
            Ok(n) => n,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        writer.write_all(&buffer[..count])?;
        total += count as u64;
    }
    Ok(total)
}

fn tunnel(left: TcpStream, right: TcpStream) -> Result<(), NetworkError> {
    let deadline = Instant::now() + TUNNEL_MAX_LIFETIME;
    let mut left_read = left.try_clone().map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    let mut right_write = right.try_clone().map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    let max_bytes = TUNNEL_MAX_BYTES;
    let outbound =
        thread::spawn(move || copy_bounded(&mut left_read, &mut right_write, max_bytes, deadline));
    let mut right_read = right;
    let mut left_write = left;
    // Drive the other direction on this thread. We ignore the result: either
    // direction ending (EOF, timeout, or byte cap) terminates the tunnel, and
    // we drop the streams so the peer sees a closed connection.
    let _ = copy_bounded(&mut right_read, &mut left_write, max_bytes, deadline);
    let _ = outbound.join();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{self, Cursor};

    fn tcp_pair() -> (TcpStream, TcpStream) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let connector = thread::spawn(move || TcpStream::connect(address).unwrap());
        let accepted = listener.accept().unwrap().0;
        (accepted, connector.join().unwrap())
    }

    #[test]
    fn occupied_listener_surfaces_network_unavailable_code() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let policy = NetworkPolicy::new(Vec::new()).unwrap();
        let err = ProxyHandle::start_on(policy, port).err().unwrap();
        assert_eq!(err.code, NetworkErrorCode::NetworkUnavailable);
    }

    #[test]
    fn dropping_proxy_terminates_already_accepted_clients() {
        let origin = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let origin_port = origin.local_addr().unwrap().port();
        let (accepted_tx, accepted_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let origin_thread = thread::spawn(move || {
            let (_stream, _) = origin.accept().unwrap();
            accepted_tx.send(()).unwrap();
            let _ = release_rx.recv_timeout(Duration::from_secs(2));
        });
        let policy = NetworkPolicy::new(vec![crate::protocol::NetworkRule {
            host: "127.0.0.1".to_string(),
            port: origin_port,
            protocol: "http".to_string(),
            allow: true,
            allow_private: true,
            escalatable: true,
        }])
        .unwrap();
        let reservation = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let proxy_port = reservation.local_addr().unwrap().port();
        drop(reservation);
        let proxy = ProxyHandle::start_on(policy, proxy_port).unwrap();
        let authorization = format!(
            "Basic {}",
            BASE64_STANDARD.encode(format!("crew:{}", proxy.password))
        );
        let mut client = TcpStream::connect((Ipv4Addr::LOCALHOST, proxy_port)).unwrap();
        client
            .write_all(
                format!(
                    "GET http://127.0.0.1:{origin_port}/ HTTP/1.1\r\n\
                     Host: 127.0.0.1:{origin_port}\r\n\
                     Proxy-Authorization: {authorization}\r\n\r\n"
                )
                .as_bytes(),
            )
            .unwrap();
        accepted_rx.recv_timeout(Duration::from_secs(1)).unwrap();

        drop(proxy);
        client
            .set_read_timeout(Some(Duration::from_millis(500)))
            .unwrap();
        let mut byte = [0_u8; 1];
        let outcome = client.read(&mut byte);
        let _ = release_tx.send(());
        origin_thread.join().unwrap();
        match outcome {
            Ok(0) => {}
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::ConnectionAborted
                        | std::io::ErrorKind::ConnectionReset
                        | std::io::ErrorKind::BrokenPipe
                ) => {}
            other => panic!("accepted client remained live after proxy drop: {other:?}"),
        }
    }

    #[test]
    fn plain_http_response_header_has_a_hard_limit() {
        let (mut upstream, mut origin) = tcp_pair();
        let (mut client, _browser) = tcp_pair();
        let writer = thread::spawn(move || {
            origin.write_all(&vec![b'A'; MAX_HEADER + 1]).unwrap();
        });

        let err = relay_http_response(
            &mut upstream,
            &mut client,
            Instant::now() + Duration::from_secs(1),
        )
        .unwrap_err();

        writer.join().unwrap();
        assert_eq!(err.code, NetworkErrorCode::SandboxDenied);
        assert!(err.message.contains("headers exceed limit"));
    }

    #[test]
    fn plain_http_request_body_obeys_the_total_deadline() {
        let (mut client, _caller) = tcp_pair();
        let (mut upstream, _origin) = tcp_pair();

        let err = copy_request_body(
            &mut client,
            &mut upstream,
            1,
            Instant::now() - Duration::from_millis(1),
        )
        .unwrap_err();

        assert_eq!(err.code, NetworkErrorCode::NetworkUnavailable);
        assert!(err.message.contains("lifetime"));
    }

    // N6 regression: the tunnel byte ceiling must actually fire. Without it a
    // peer streaming data could keep a proxy slot alive for the full lifetime.
    #[test]
    fn copy_bounded_rejects_when_byte_limit_reached() {
        let data = vec![0u8; 1024];
        let mut reader = Cursor::new(data);
        let mut writer = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(60);
        let result = copy_bounded(&mut reader, &mut writer, 100, deadline);
        let err = result.unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::Other);
        assert!(err.to_string().contains("byte limit"));
    }

    // N6 regression: an expired deadline must abort immediately even if the
    // peer is still streaming. `io::repeat()` never EOFs, so only the deadline
    // check can terminate the loop.
    #[test]
    fn copy_bounded_aborts_on_expired_deadline() {
        let mut reader = io::repeat(0u8);
        let mut writer = Vec::new();
        let deadline = Instant::now() - Duration::from_secs(1);
        let result = copy_bounded(&mut reader, &mut writer, u64::MAX, deadline);
        let err = result.unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::TimedOut);
        assert!(err.to_string().contains("lifetime"));
    }

    #[test]
    fn copy_bounded_completes_on_eof_under_limits() {
        let data = vec![0u8; 64];
        let mut reader = Cursor::new(data);
        let mut writer = Vec::new();
        let deadline = Instant::now() + Duration::from_secs(60);
        let copied = copy_bounded(&mut reader, &mut writer, 1024, deadline).unwrap();
        assert_eq!(copied, 64);
        assert_eq!(writer.len(), 64);
    }

    #[test]
    fn absolute_target_must_match_the_single_host_header() {
        let matching =
            b"GET http://allowed.example:8080/ HTTP/1.1\r\nHost: allowed.example:8080\r\n\r\n";
        validate_proxy_host(matching, "allowed.example", 8080, 80).unwrap();

        let mismatched =
            b"GET http://allowed.example:8080/ HTTP/1.1\r\nHost: denied.example:8080\r\n\r\n";
        let error = validate_proxy_host(mismatched, "allowed.example", 8080, 80).unwrap_err();
        assert_eq!(error.code, NetworkErrorCode::SandboxDenied);

        let duplicate = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\nHost: allowed.example:443\r\n\r\n";
        assert!(validate_proxy_host(duplicate, "allowed.example", 443, 443).is_err());
    }

    fn client_hello(host: Option<&str>) -> Vec<u8> {
        let mut body = vec![0x03, 0x03];
        body.extend_from_slice(&[0_u8; 32]);
        body.push(0);
        body.extend_from_slice(&[0, 2, 0x13, 0x01]);
        body.extend_from_slice(&[1, 0]);
        let mut extensions = Vec::new();
        if let Some(host) = host {
            let host = host.as_bytes();
            let list_len = 1 + 2 + host.len();
            let extension_len = 2 + list_len;
            extensions.extend_from_slice(&[0, 0]);
            extensions.extend_from_slice(&(extension_len as u16).to_be_bytes());
            extensions.extend_from_slice(&(list_len as u16).to_be_bytes());
            extensions.push(0);
            extensions.extend_from_slice(&(host.len() as u16).to_be_bytes());
            extensions.extend_from_slice(host);
        }
        body.extend_from_slice(&(extensions.len() as u16).to_be_bytes());
        body.extend_from_slice(&extensions);
        body
    }

    #[test]
    fn connect_client_hello_sni_must_match_authorized_authority() {
        validate_client_hello(&client_hello(Some("allowed.example")), "allowed.example").unwrap();
        assert!(
            validate_client_hello(&client_hello(Some("denied.example")), "allowed.example")
                .is_err()
        );
        assert!(validate_client_hello(&client_hello(None), "allowed.example").is_err());
        validate_client_hello(&client_hello(None), "93.184.216.34").unwrap();
    }

    #[test]
    fn managed_proxy_requires_one_constant_time_authorization_header() {
        let expected = "Basic Y3JldzpzZWNyZXQ=";
        let valid = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\nProxy-Authorization: Basic Y3JldzpzZWNyZXQ=\r\n\r\n";
        validate_proxy_authorization(valid, expected).unwrap();

        let missing = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n";
        assert!(validate_proxy_authorization(missing, expected).is_err());
        let duplicate = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\nProxy-Authorization: Basic Y3JldzpzZWNyZXQ=\r\nProxy-Authorization: Basic Y3JldzpzZWNyZXQ=\r\n\r\n";
        assert!(validate_proxy_authorization(duplicate, expected).is_err());
        let wrong = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\nProxy-Authorization: Basic Y3JldzphdHRhY2tlcg==\r\n\r\n";
        assert!(validate_proxy_authorization(wrong, expected).is_err());
    }

    #[test]
    fn rewritten_plain_http_never_leaks_proxy_credentials_upstream() {
        let header = b"GET http://allowed.example/path HTTP/1.1\r\nHost: allowed.example\r\nProxy-Authorization: Basic c2VjcmV0\r\nProxy-Connection: keep-alive\r\nConnection: keep-alive\r\n\r\n";
        let rewritten = rewrite_request_line(header, "GET", "/path", "HTTP/1.1").unwrap();
        let text = String::from_utf8(rewritten).unwrap().to_ascii_lowercase();
        assert!(!text.contains("proxy-authorization"));
        assert!(!text.contains("proxy-connection"));
        assert_eq!(text.matches("connection: close\r\n").count(), 1);
    }

    #[test]
    fn plain_http_request_framing_is_bounded_and_unambiguous() {
        let valid =
            b"POST http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nContent-Length: 4\r\n\r\n";
        assert_eq!(request_body_length(valid).unwrap(), 4);
        let duplicate = b"POST http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nContent-Length: 4\r\nContent-Length: 4\r\n\r\n";
        assert!(request_body_length(duplicate).is_err());
        let chunked = b"POST http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nTransfer-Encoding: chunked\r\n\r\n";
        assert!(request_body_length(chunked).is_err());
        let oversized = format!(
            "POST http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\nContent-Length: {}\r\n\r\n",
            HTTP_MAX_REQUEST_BYTES + 1
        );
        assert!(request_body_length(oversized.as_bytes()).is_err());
    }

    #[test]
    fn websocket_upgrade_is_forwarded_only_as_a_valid_upgrade() {
        let header = b"GET http://allowed.example/socket HTTP/1.1\r\n\
                       Host: allowed.example\r\n\
                       Connection: keep-alive, Upgrade\r\n\
                       Upgrade: websocket\r\n\
                       Sec-WebSocket-Key: key\r\n\r\n";
        assert!(websocket_request(header, "GET").unwrap());
        let rewritten =
            rewrite_websocket_request_line(header, "GET", "/socket", "HTTP/1.1").unwrap();
        let text = String::from_utf8(rewritten).unwrap().to_ascii_lowercase();
        assert!(text.contains("upgrade: websocket\r\n"));
        assert!(text.contains("connection: upgrade\r\n"));
        assert!(!text.contains("proxy-authorization"));
    }

    #[test]
    fn websocket_upgrade_rejects_incomplete_request_or_response() {
        let invalid_request =
            b"POST http://allowed.example/socket HTTP/1.1\r\nConnection: Upgrade\r\n\
              Upgrade: websocket\r\n\r\n";
        assert!(websocket_request(invalid_request, "POST").is_err());

        let invalid_response = b"HTTP/1.1 200 OK\r\n\r\n";
        assert!(validate_websocket_response(invalid_response).is_err());
        let valid_response = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\
              Connection: Upgrade\r\n\r\n";
        validate_websocket_response(valid_response).unwrap();
    }

    #[test]
    fn proxy_authority_rejects_zero_port() {
        assert!(split_authority("allowed.example:0", 443).is_err());
        assert!(split_host_header_authority("allowed.example:0", 443).is_err());
        assert!(split_authority("[::1]suffix", 443).is_err());
    }
}
