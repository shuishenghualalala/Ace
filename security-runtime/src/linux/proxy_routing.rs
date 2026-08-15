//! Private Unix-socket bridge between a host proxy and a bwrap network namespace.

use std::io::{self, Read, Write};
use std::net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream};
use std::os::fd::AsRawFd;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicUsize, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

pub const INNER_PROXY_PORT: u16 = 43119;
pub const INNER_SOCKET_PATH: &str = "/run/ace-network/proxy.sock";
const MAX_BRIDGE_CONNECTIONS: usize = 64;
const BRIDGE_IO_TIMEOUT: Duration = Duration::from_secs(30);
const BRIDGE_MAX_LIFETIME: Duration = Duration::from_secs(600);
const BRIDGE_MAX_BYTES: u64 = 256 * 1024 * 1024;

pub struct InnerBridge {
    pid: libc::pid_t,
}

impl InnerBridge {
    pub fn stop(&mut self) {
        if self.pid <= 0 {
            return;
        }
        unsafe {
            libc::kill(self.pid, libc::SIGKILL);
            let mut status = 0;
            while libc::waitpid(self.pid, &mut status, 0) < 0 {
                if io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
                    break;
                }
            }
        }
        self.pid = -1;
    }
}

impl Drop for InnerBridge {
    fn drop(&mut self) {
        self.stop();
    }
}

pub struct HostBridge {
    pub socket_dir: PathBuf,
    stopped: Arc<AtomicBool>,
    worker: Option<thread::JoinHandle<()>>,
}

impl HostBridge {
    pub fn start(proxy_address: SocketAddr, proxy_authorization: String) -> Result<Self, String> {
        let socket_dir = std::env::temp_dir().join(format!(
            "ace-network-{}-{}",
            std::process::id(),
            rand::random::<u64>()
        ));
        std::fs::create_dir(&socket_dir)
            .map_err(|error| format!("cannot create proxy bridge directory: {error}"))?;
        use std::os::unix::fs::PermissionsExt;
        let setup = (|| {
            std::fs::set_permissions(&socket_dir, std::fs::Permissions::from_mode(0o700))
                .map_err(|error| format!("cannot protect proxy bridge directory: {error}"))?;
            let listener = UnixListener::bind(socket_dir.join("proxy.sock"))
                .map_err(|error| format!("cannot bind proxy bridge socket: {error}"))?;
            listener
                .set_nonblocking(true)
                .map_err(|error| format!("cannot configure proxy bridge socket: {error}"))?;
            Ok(listener)
        })();
        let listener = match setup {
            Ok(listener) => listener,
            Err(error) => {
                let _ = std::fs::remove_dir_all(&socket_dir);
                return Err(error);
            }
        };
        let stopped = Arc::new(AtomicBool::new(false));
        let stop = Arc::clone(&stopped);
        let active = Arc::new(AtomicUsize::new(0));
        let pinned_peer = Arc::new(AtomicI32::new(0));
        let runtime_pid = std::process::id() as libc::pid_t;
        let worker = thread::Builder::new()
            .name("ace-linux-proxy-bridge".to_string())
            .spawn(move || {
                while !stop.load(Ordering::Acquire) {
                    match listener.accept() {
                        Ok((inside, _)) => {
                            if !authorized_bridge_peer(&inside, runtime_pid, &pinned_peer) {
                                continue;
                            }
                            if active.fetch_add(1, Ordering::AcqRel) >= MAX_BRIDGE_CONNECTIONS {
                                active.fetch_sub(1, Ordering::AcqRel);
                                continue;
                            }
                            let active = Arc::clone(&active);
                            let proxy_authorization = proxy_authorization.clone();
                            thread::spawn(move || {
                                let mut inside = inside;
                                if inside.set_read_timeout(Some(BRIDGE_IO_TIMEOUT)).is_ok()
                                    && inside.set_write_timeout(Some(BRIDGE_IO_TIMEOUT)).is_ok()
                                {
                                    if let Ok(header) = read_and_authorize_proxy_header(
                                        &mut inside,
                                        &proxy_authorization,
                                    ) {
                                        if let Ok(mut proxy) = TcpStream::connect_timeout(
                                            &proxy_address,
                                            BRIDGE_IO_TIMEOUT,
                                        ) {
                                            if configure_bridge_streams(&inside, &proxy).is_ok()
                                                && proxy.write_all(&header).is_ok()
                                            {
                                                let _ = tunnel_unix_tcp(inside, proxy);
                                            }
                                        }
                                    }
                                }
                                active.fetch_sub(1, Ordering::AcqRel);
                            });
                        }
                        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(10));
                        }
                        Err(_) => break,
                    }
                }
            });
        let worker = match worker {
            Ok(worker) => worker,
            Err(error) => {
                let _ = std::fs::remove_dir_all(&socket_dir);
                return Err(format!("cannot start proxy bridge worker: {error}"));
            }
        };
        Ok(Self {
            socket_dir,
            stopped,
            worker: Some(worker),
        })
    }
}

impl Drop for HostBridge {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        let _ = std::fs::remove_dir_all(&self.socket_dir);
    }
}

/// Fork a tiny namespace-local loopback bridge before the command seccomp filter is installed.
pub fn start_inner_bridge(socket_path: &Path) -> Result<InnerBridge, String> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, INNER_PROXY_PORT))
        .map_err(|error| format!("cannot bind inner proxy bridge: {error}"))?;
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(format!(
            "cannot fork inner proxy bridge: {}",
            io::Error::last_os_error()
        ));
    }
    if pid == 0 {
        let active = Arc::new(AtomicUsize::new(0));
        loop {
            let Ok((inside, _)) = listener.accept() else {
                std::process::exit(0);
            };
            if active.fetch_add(1, Ordering::AcqRel) >= MAX_BRIDGE_CONNECTIONS {
                active.fetch_sub(1, Ordering::AcqRel);
                continue;
            }
            let Ok(host) = UnixStream::connect(socket_path) else {
                active.fetch_sub(1, Ordering::AcqRel);
                continue;
            };
            let active = Arc::clone(&active);
            thread::spawn(move || {
                if configure_inner_streams(&inside, &host).is_ok() {
                    let _ = tunnel_tcp_unix(inside, host);
                }
                active.fetch_sub(1, Ordering::AcqRel);
            });
        }
    }
    drop(listener);
    Ok(InnerBridge { pid })
}

pub fn proxy_url() -> String {
    format!("http://127.0.0.1:{INNER_PROXY_PORT}")
}

fn read_and_authorize_proxy_header(
    stream: &mut UnixStream,
    authorization: &str,
) -> io::Result<Vec<u8>> {
    const MAX_HEADER_BYTES: usize = 64 * 1024;
    let mut header = Vec::new();
    let mut byte = [0_u8; 1];
    while header.len() < MAX_HEADER_BYTES {
        let count = stream.read(&mut byte)?;
        if count == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "bridge request ended before headers",
            ));
        }
        header.push(byte[0]);
        if header.ends_with(b"\r\n\r\n") {
            return add_proxy_authorization(&header, authorization);
        }
    }
    Err(io::Error::new(
        io::ErrorKind::InvalidData,
        "bridge proxy headers exceed limit",
    ))
}

fn add_proxy_authorization(header: &[u8], authorization: &str) -> io::Result<Vec<u8>> {
    if !header.ends_with(b"\r\n\r\n")
        || authorization.is_empty()
        || authorization
            .bytes()
            .any(|byte| byte <= 0x20 || byte == 0x7f)
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid bridge proxy authorization",
        ));
    }
    let text = std::str::from_utf8(header)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "proxy header is not UTF-8"))?;
    if text.split("\r\n").skip(1).any(|line| {
        line.split_once(':')
            .is_some_and(|(name, _)| name.eq_ignore_ascii_case("proxy-authorization"))
    }) {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "sandbox cannot supply proxy authorization",
        ));
    }
    let mut result = header[..header.len() - 2].to_vec();
    result.extend_from_slice(b"Proxy-Authorization: ");
    result.extend_from_slice(authorization.as_bytes());
    result.extend_from_slice(b"\r\n\r\n");
    Ok(result)
}

fn tunnel_unix_tcp(mut unix: UnixStream, mut tcp: TcpStream) -> io::Result<()> {
    let deadline = Instant::now() + BRIDGE_MAX_LIFETIME;
    let mut unix_read = unix.try_clone()?;
    let mut tcp_write = tcp.try_clone()?;
    let outbound = thread::spawn(move || {
        copy_bounded(&mut unix_read, &mut tcp_write, BRIDGE_MAX_BYTES, deadline)
    });
    copy_bounded(&mut tcp, &mut unix, BRIDGE_MAX_BYTES, deadline)?;
    outbound.join().unwrap_or(Ok(0))?;
    Ok(())
}

fn tunnel_tcp_unix(mut tcp: TcpStream, mut unix: UnixStream) -> io::Result<()> {
    let deadline = Instant::now() + BRIDGE_MAX_LIFETIME;
    let mut tcp_read = tcp.try_clone()?;
    let mut unix_write = unix.try_clone()?;
    let outbound = thread::spawn(move || {
        copy_bounded(&mut tcp_read, &mut unix_write, BRIDGE_MAX_BYTES, deadline)
    });
    copy_bounded(&mut unix, &mut tcp, BRIDGE_MAX_BYTES, deadline)?;
    outbound.join().unwrap_or(Ok(0))?;
    Ok(())
}

fn configure_bridge_streams(unix: &UnixStream, tcp: &TcpStream) -> io::Result<()> {
    unix.set_read_timeout(Some(BRIDGE_IO_TIMEOUT))?;
    unix.set_write_timeout(Some(BRIDGE_IO_TIMEOUT))?;
    tcp.set_read_timeout(Some(BRIDGE_IO_TIMEOUT))?;
    tcp.set_write_timeout(Some(BRIDGE_IO_TIMEOUT))
}

fn configure_inner_streams(tcp: &TcpStream, unix: &UnixStream) -> io::Result<()> {
    tcp.set_read_timeout(Some(BRIDGE_IO_TIMEOUT))?;
    tcp.set_write_timeout(Some(BRIDGE_IO_TIMEOUT))?;
    unix.set_read_timeout(Some(BRIDGE_IO_TIMEOUT))?;
    unix.set_write_timeout(Some(BRIDGE_IO_TIMEOUT))
}

fn copy_bounded<R: Read, W: Write>(
    reader: &mut R,
    writer: &mut W,
    max_bytes: u64,
    deadline: Instant,
) -> io::Result<u64> {
    let mut total = 0_u64;
    let mut buffer = [0_u8; 8192];
    loop {
        if total >= max_bytes {
            return Err(io::Error::other("bridge byte limit exceeded"));
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "bridge lifetime exceeded",
            ));
        }
        let remaining = (max_bytes - total).min(buffer.len() as u64) as usize;
        let count = match reader.read(&mut buffer[..remaining]) {
            Ok(0) => return Ok(total),
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        writer.write_all(&buffer[..count])?;
        total += count as u64;
    }
}

fn authorized_bridge_peer(
    stream: &UnixStream,
    runtime_pid: libc::pid_t,
    pinned_peer: &AtomicI32,
) -> bool {
    let Some((peer_pid, peer_uid)) = unix_peer_credentials(stream) else {
        return false;
    };
    if peer_pid <= 0
        || peer_uid != unsafe { libc::geteuid() }
        || !process_descends_from(peer_pid, runtime_pid)
    {
        return false;
    }
    let pinned = pinned_peer.load(Ordering::Acquire);
    if pinned == peer_pid {
        return true;
    }
    pinned == 0
        && pinned_peer
            .compare_exchange(0, peer_pid, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
}

fn unix_peer_credentials(stream: &UnixStream) -> Option<(libc::pid_t, libc::uid_t)> {
    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut credentials as *mut libc::ucred as *mut libc::c_void,
            &mut length,
        )
    };
    (result == 0 && length as usize == std::mem::size_of::<libc::ucred>())
        .then_some((credentials.pid, credentials.uid))
}

fn process_descends_from(mut pid: libc::pid_t, ancestor: libc::pid_t) -> bool {
    for _ in 0..128 {
        if pid == ancestor {
            return true;
        }
        if pid <= 1 {
            return false;
        }
        let Ok(status) = std::fs::read_to_string(format!("/proc/{pid}/status")) else {
            return false;
        };
        let Some(parent) = parse_parent_pid(&status) else {
            return false;
        };
        if parent == pid {
            return false;
        }
        pid = parent;
    }
    false
}

fn parse_parent_pid(status: &str) -> Option<libc::pid_t> {
    let values = status
        .lines()
        .filter_map(|line| line.strip_prefix("PPid:"))
        .collect::<Vec<_>>();
    if values.len() != 1 {
        return None;
    }
    values[0].trim().parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::time::Instant;

    #[test]
    fn proc_parent_parser_requires_one_numeric_ppid() {
        assert_eq!(
            parse_parent_pid("Name:\ttask\nPid:\t42\nPPid:\t17\nUid:\t1000\n"),
            Some(17)
        );
        assert_eq!(parse_parent_pid("Name:\ttask\nPPid:\tnot-a-pid\n"), None);
        assert_eq!(parse_parent_pid("Name:\ttask\n"), None);
    }

    #[test]
    fn bridge_copy_stops_at_byte_and_lifetime_limits() {
        let mut reader = Cursor::new(vec![0_u8; 32]);
        let mut writer = Vec::new();
        assert!(copy_bounded(
            &mut reader,
            &mut writer,
            8,
            Instant::now() + Duration::from_secs(1),
        )
        .is_err());

        let mut reader = Cursor::new(vec![0_u8; 1]);
        let mut writer = Vec::new();
        let error = copy_bounded(
            &mut reader,
            &mut writer,
            8,
            Instant::now() - Duration::from_secs(1),
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn host_bridge_injects_exactly_one_private_proxy_credential() {
        let header = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n";
        let injected = add_proxy_authorization(header, "Basic private-token").unwrap();
        let text = String::from_utf8(injected).unwrap();
        assert_eq!(text.matches("Proxy-Authorization:").count(), 1);
        assert!(text.contains("Proxy-Authorization: Basic private-token\r\n"));

        let supplied = b"CONNECT allowed.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\nProxy-Authorization: attacker\r\n\r\n";
        assert!(add_proxy_authorization(supplied, "Basic private-token").is_err());
    }
}
