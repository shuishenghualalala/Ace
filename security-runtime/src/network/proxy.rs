use std::io::{Read, Write};
#[cfg(target_os = "linux")]
use std::net::SocketAddr;
use std::net::{IpAddr, Ipv4Addr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use super::connector;
use super::policy::{NetworkError, NetworkPolicy};
use crate::protocol::NetworkErrorCode;

const MAX_HEADER: usize = 64 * 1024;
const MAX_CONNECTIONS: usize = 64;
const IO_TIMEOUT: Duration = Duration::from_secs(30);
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

pub struct ProxyHandle {
    #[cfg(target_os = "linux")]
    address: SocketAddr,
    stopped: Arc<AtomicBool>,
}

impl ProxyHandle {
    #[cfg(target_os = "linux")]
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
        #[cfg(target_os = "linux")]
        let address = listener.local_addr().map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        listener.set_nonblocking(true).map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&stopped);
        let active = Arc::new(AtomicUsize::new(0));
        thread::spawn(move || {
            while !stop.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((stream, peer)) if peer.ip() == IpAddr::V4(Ipv4Addr::LOCALHOST) => {
                        if active.fetch_add(1, Ordering::AcqRel) >= MAX_CONNECTIONS {
                            active.fetch_sub(1, Ordering::AcqRel);
                            continue;
                        }
                        let policy = policy.clone();
                        let active = Arc::clone(&active);
                        thread::spawn(move || {
                            let _ = serve(stream, &policy);
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
            #[cfg(target_os = "linux")]
            address,
            stopped,
        })
    }

    #[cfg(target_os = "linux")]
    pub fn address(&self) -> SocketAddr {
        self.address
    }
}

impl Drop for ProxyHandle {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
    }
}

fn serve(mut client: TcpStream, policy: &NetworkPolicy) -> Result<(), NetworkError> {
    client.set_read_timeout(Some(IO_TIMEOUT)).map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    client
        .set_write_timeout(Some(IO_TIMEOUT))
        .map_err(|error| {
            NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
        })?;
    let header = read_header(&mut client)?;
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
    if fields[0].eq_ignore_ascii_case("CONNECT") {
        let (host, port) = split_authority(fields[1], 443)?;
        let upstream = connector::connect(policy, &host, port, "https", IO_TIMEOUT)?;
        client
            .write_all(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            .map_err(|error| {
                NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
            })?;
        return tunnel(client, upstream);
    }
    let (host, port, path) = parse_absolute_http_target(fields[1])?;
    let mut upstream = connector::connect(policy, &host, port, "http", IO_TIMEOUT)?;
    let rewritten = rewrite_request_line(&header, fields[0], &path, fields[2])?;
    upstream.write_all(&rewritten).map_err(|error| {
        NetworkError::new(NetworkErrorCode::NetworkUnavailable, error.to_string())
    })?;
    tunnel(client, upstream)
}

fn read_header(stream: &mut TcpStream) -> Result<Vec<u8>, NetworkError> {
    let mut result = Vec::new();
    let mut byte = [0_u8; 1];
    while result.len() < MAX_HEADER {
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

fn split_authority(value: &str, default_port: u16) -> Result<(String, u16), NetworkError> {
    if let Some(host) = value.strip_prefix('[') {
        let (host, tail) = host.split_once(']').ok_or_else(|| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid IPv6 authority")
        })?;
        let port = tail
            .strip_prefix(':')
            .map(str::parse)
            .transpose()
            .map_err(|_| NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy port"))?
            .unwrap_or(default_port);
        return Ok((host.to_string(), port));
    }
    let (host, port) = value.rsplit_once(':').ok_or_else(|| {
        NetworkError::new(
            NetworkErrorCode::SandboxDenied,
            "proxy target must include a port",
        )
    })?;
    Ok((
        host.to_string(),
        port.parse().map_err(|_| {
            NetworkError::new(NetworkErrorCode::SandboxDenied, "invalid proxy port")
        })?,
    ))
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
    let boundary = header
        .windows(2)
        .position(|value| value == b"\r\n")
        .ok_or_else(|| {
            NetworkError::new(
                NetworkErrorCode::SandboxDenied,
                "missing request-line boundary",
            )
        })?;
    let mut result = format!("{method} {path} {version}").into_bytes();
    result.extend_from_slice(&header[boundary..]);
    Ok(result)
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
        let count = match reader.read(&mut buffer) {
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

    #[test]
    fn occupied_listener_surfaces_network_unavailable_code() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let policy = NetworkPolicy::new(Vec::new()).unwrap();
        let err = ProxyHandle::start_on(policy, port).err().unwrap();
        assert_eq!(err.code, NetworkErrorCode::NetworkUnavailable);
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
}
